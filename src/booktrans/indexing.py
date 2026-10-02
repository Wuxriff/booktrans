"""Linked EPUB indexes: structure, term translation and deterministic assembly."""
import copy
import json
import os
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache

from . import lang
from .tune import AGY_CAP, INDEX_BATCH, INDEX_CONTEXT_BLOCKS, INDEX_CONTEXT_CHARS, INDEX_LINK_SHARE

VERSION = 1
HEAD = re.compile(
    r"^(?:(?:name|subject|author|general|organization)\s+)?index\b"
    r"|^(?:предметный\s+|именной\s+)?указатель\b"
    r"|^(?:索引|indeks|stichwortverzeichnis|índice|indice)\b", re.I)
SEE = re.compile(
    r"\b(?:see\s+also|см\.\s*также|siehe\s+auch|voir\s+aussi|véase\s+también)\b"
    r"|\b(?:see|siehe|voir|véase)\b|\bсм\.(?!\w)", re.I)


def plain(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def doc_roles(root):
    """Element identities -> index roles, before containers lose their own text."""
    nodes = list(root.iter())
    parents = {ch: el for el in nodes for ch in el}
    semantic = {}
    for i, el in enumerate(nodes):
        kinds = (el.get("{http://www.idpf.org/2007/ops}type", "") + " "
                 + el.get("role", "")).split()
        if "index" in kinds or "doc-index" in kinds:
            semantic.update({child: str(i) for child in el.iter()})
    roles, section, active, level, stack = {}, "", False, 0, []
    owner = None
    for i, el in enumerate(nodes):
        tag = _tag(el)
        text = " ".join(el.itertext()).strip()
        if semantic.get(el) != owner:
            owner = semantic.get(el)
            section, active, stack = "semantic_" + owner if owner is not None else "", False, []
        heading = tag in ("h1", "h2", "h3", "h4", "h5", "h6")
        named = tag in ("p", "div") and bool(HEAD.fullmatch(text))
        if (heading or named) and HEAD.fullmatch(text):
            section, active, level, stack = str(i), True, int(tag[1]) if heading else 1, []
            roles[id(el)] = {"key": str(i), "section": section, "heading": True}
            continue
        if heading and active and int(tag[1]) <= level and not re.fullmatch(r"\W*[^\W\d_]\W*", text):
            active = False
        if not (active or el in semantic):
            continue
        if not section:
            section = "semantic"
        if heading:
            roles[id(el)] = {"key": str(i), "section": section, "divider": True}
            stack = []
            continue
        if tag not in ("li", "p"):
            continue
        p = parents.get(el)
        ancestor = None
        while p is not None:
            if _tag(p) == "li" and id(p) in roles:
                ancestor = p
                break
            p = parents.get(p)
        if tag == "p" and ancestor is not None:
            continue                         # its owning li reads it once
        parent = roles[id(ancestor)]["key"] if ancestor is not None else ""
        if tag == "p":
            cls = el.get("class", "").lower()
            m = re.search(r"index[-_ ]?(?:sub)?([1-9])", cls)
            depth = int(m.group(1)) - 1 if m else len(re.findall("sub", cls))
            stack = stack[:depth]
            parent = stack[-1] if stack else ""
            stack.append(str(i))
        roles[id(el)] = {"key": str(i), "section": section, "parent": parent}
    return roles


def own_element(el):
    """A parent entry includes its label, but not the nested entries."""
    node = copy.deepcopy(el)
    for parent in node.iter():
        for ch in list(parent):
            if _tag(ch) in ("ul", "ol"):
                parent.remove(ch)
    return node


def split_entry(text):
    """Separate the sortable term from locators and see-references."""
    numeric = re.search(r"<a\d+>\s*[\dⅰ-ⅿivxlcdm]+(?:[–—-][\dⅰ-ⅿivxlcdm]+)?\s*</a\d+>", text, re.I)
    # Plain page numbers also count, so mixed indexes have an honest denominator.
    bare = re.search(r",\s+(?=\d+(?:\s*[,–—-]|\s*$))", text)
    see = SEE.search(plain(text))
    see_markup = re.search(r"(?:<(?:i|b)>\s*)?(?:см\.(?:\s*также\b)?|(?:see(?:\s+also)?|siehe(?:\s+auch)?|voir(?:\s+aussi)?|véase(?:\s+también)?)\b)", text, re.I) if see else None
    cuts = [m.start() for m in (numeric, bare, see_markup) if m]
    at = min(cuts) if cuts else len(text)
    term = text[:at].rstrip(" ,.;:")
    tail = text[len(term):]
    return term, tail


def finish_roles(blocks):
    """Convert document-local entry identities to persistent block IDs."""
    ids = {b["index"]["key"]: b["id"] for b in blocks if b.get("index")}
    for b in blocks:
        info = b.get("index")
        if not info:
            continue
        info["section"] = ids.get(info["section"], blocks[0]["id"])
        info["parent"] = ids.get(info.get("parent"), "")
        info.pop("key", None)
        if not (info.get("heading") or info.get("divider")):
            info["term"], info["tail"] = split_entry(b["text"])
        else:
            info["term"] = b["text"]
        b["asis"] = True                    # not prose; dedicated index pass


def selected(blocks, mode="auto"):
    ids = {b["id"] for b in blocks if not b.get("drop") or b.get("index")}
    sections = defaultdict(list)
    for b in blocks:
        if b.get("index"):
            sections[b["index"]["section"]].append(b)
    keep = set()
    for sec, entries in sections.items():
        parent_ids = {b["index"].get("parent") for b in entries}
        records = [b for b in entries if not b["index"].get("heading")
                   and not b["index"].get("divider")
                   and (b.get("links") or b["index"].get("tail") or b["id"] not in parent_ids)]
        linked = sum(any(u.startswith("#") and u[1:] in ids for u in b.get("links", [])) for b in records)
        if mode != "omit" and linked and (mode != "auto" or linked / max(1, len(records)) >= INDEX_LINK_SHARE):
            keep.add(sec)
    return keep


def mark(blocks, mode="auto"):
    keep = selected(blocks, mode)
    for b in blocks:
        if b.get("index"):
            b["asis"] = True
            if b["index"]["section"] in keep:
                b.pop("drop", None)
            else:
                b["drop"] = True
    return keep


def restore(path, work, blocks):
    """Upgrade cached EPUB indexes without renumbering or rereading the prose."""
    from . import extract as E
    styles = _load(os.path.join(work, "structure.json"))
    _, fresh, _, _ = E._epub(path, styles)
    sections = defaultdict(list)
    for b in fresh:
        if b.get("index"):
            sections[b["index"]["section"]].append(b)
    cached = {b["id"]: b for b in blocks}
    anchors, texts = defaultdict(set), defaultdict(set)
    for b in blocks:
        for a in b.get("anchors", []):
            anchors[a].add(b["id"])
        texts[(b["kind"], b["text"])].add(b["id"])
    remap = {}
    for b in fresh:
        if b.get("index"):
            remap[b["id"]] = b["id"].replace(".b", ".x")
        elif b["id"] in cached and cached[b["id"]]["text"] == b["text"]:
            remap[b["id"]] = b["id"]
        else:
            matches = set().union(*(anchors[a] for a in b.get("anchors", [])))
            if len(matches) != 1:
                matches = texts[(b["kind"], b["text"])]
            if len(matches) == 1:
                remap[b["id"]] = next(iter(matches))
    replacements = []
    for sec, entries in sections.items():
        name = plain(next((b["text"] for b in entries if b["index"].get("heading")), "Index"))
        start = next((i for i, b in enumerate(blocks) if b["kind"] in ("title", "subtitle") and plain(b["text"]).casefold() == name.casefold()), None)
        if start is not None:
            end = next((i for i in range(start + 1, len(blocks)) if blocks[i]["kind"] == "title" and not re.fullmatch(r"\W*[^\W\d_]\W*", plain(blocks[i]["text"]))), len(blocks))
        else:
            before = fresh[:fresh.index(entries[0])]
            prev = next((remap[b["id"]] for b in reversed(before) if not b.get("index") and b["id"] in remap), None)
            start = next((i + 1 for i, b in enumerate(blocks) if b["id"] == prev), len(blocks))
            end = start
        new = copy.deepcopy(entries)
        for b in new:
            b["id"] = remap[b["id"]]
            info = b["index"]
            info["section"] = remap[info["section"]]
            info["parent"] = remap.get(info.get("parent"), "")
            urls = []
            for u in b.get("links", []):
                if u.startswith("#"):
                    if u[1:] not in remap:
                        raise E.BadBook(f"Cannot restore index target {u}; the cached book has different source blocks")
                    u = "#" + remap[u[1:]]
                urls.append(u)
            if urls:
                b["links"] = urls
        replacements.append((start, end, new))
    out = copy.deepcopy(blocks)
    for start, end, new in sorted(replacements, reverse=True, key=lambda x: x[0]):
        out[start:end] = new
    # Prose references to an index entry must follow the restored entry as well.
    index_ids = {b["id"] for b in fresh if b.get("index")}
    source_by_id = {remap[b["id"]]: b for b in fresh if not b.get("index") and b["id"] in remap}
    for b in out:
        source = source_by_id.get(b["id"])
        if source and len(source.get("links", [])) == len(b.get("links", [])):
            for i, u in enumerate(source.get("links", [])):
                if u.startswith("#") and u[1:] in index_ids:
                    b["links"][i] = "#" + remap[u[1:]]
    return out


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def glossary(work, to):
    from . import pipeline as P
    ref = P.lpath(work, "scout.md", to)
    text = ""
    if os.path.exists(ref):
        with open(ref, encoding="utf-8") as stream:
            text = stream.read()
    known = {}
    for sec, _, key, line, kind in P._ref_scan(text):
        if sec in P.REF_ENTITY and kind == "row" and line.strip().startswith("|"):
            cells = P._cells(line)
            if len(cells) >= 2 and key and cells[1] and not cells[1].startswith("("):
                known.setdefault(plain(key).casefold(), cells[1])
    state = _load(P.lpath(work, "state.json", to))
    for k in sorted(state.get("terms", {}), key=int):
        for line in state["terms"][k].splitlines():
            if "=" in line:
                source, target = (x.strip() for x in line.split("=", 1))
                if source and target and target != "—":
                    known.setdefault(source.casefold(), target)
    for rule in _load(P.lpath(work, "fixups.json", to)).get("rules", []):
        if not rule.get("blocks"):
            for source, target in rule.get("pairs", {}).items():
                known = {k: v.replace(source, target) for k, v in known.items()}
    return known


def _requests(blocks):
    out = {b["id"]: b["index"]["term"] for b in blocks
           if b.get("index") and not b.get("drop") and not b["index"].get("divider")}
    if out:
        out.update({"_see": "see", "_see_also": "see also"})
    return out


def _stamp(term, known):
    from .pipeline import fingerprint
    return fingerprint(json.dumps([term, known], sort_keys=True, ensure_ascii=False))


def _contexts(work, blocks, to):
    """Use final prose near link destinations, not a literal term-match test."""
    from . import pipeline as P
    translated, _ = P.all_translations(work, to)
    for rule in _load(P.lpath(work, "fixups.json", to)).get("rules", []):
        for source, target in rule.get("pairs", {}).items():
            for bid in rule.get("blocks") or list(translated):
                if bid in translated:
                    translated[bid] = translated[bid].replace(source, target)
    by_id = {b["id"]: b for b in blocks}
    positions = {b["id"]: i for i, b in enumerate(blocks)}
    contexts = {}
    for b in blocks:
        if not b.get("index") or b.get("drop") or b["index"].get("divider"):
            continue
        context = {}
        parent = by_id.get(b["index"].get("parent"))
        if parent:
            context["parent"] = parent["index"]["term"]
        for url in b.get("links", []):
            target = by_id.get(url[1:]) if url.startswith("#") else None
            if not target or target.get("index"):
                continue
            passages = []
            for candidate in blocks[positions[target["id"]]:]:
                if candidate.get("index") or candidate.get("drop"):
                    break
                if candidate["id"] in translated and candidate["kind"] == "p":
                    passages.append({"source": plain(candidate["text"])[:INDEX_CONTEXT_CHARS],
                                     "translation": plain(translated[candidate["id"]])[:INDEX_CONTEXT_CHARS]})
                if len(passages) >= INDEX_CONTEXT_BLOCKS:
                    break
            if passages:
                context["passages"] = passages
                break                       # one useful destination per term
        if context:
            contexts[b["id"]] = context
    return contexts


def _inputs(work, blocks, to):
    from . import pipeline as P
    known = glossary(work, to)
    contexts = _contexts(work, blocks, to)
    stamp = P.fingerprint(json.dumps(known, ensure_ascii=False, sort_keys=True))
    hashes = {bid: _stamp(term, [stamp, contexts.get(bid)])
              for bid, term in _requests(blocks).items()}
    return known, contexts, hashes


def _batches(todo, contexts, known, to, system):
    """Count the complete UTF-8 payload, including system and envelope."""
    from . import pipeline as P
    from .agent import Fatal

    def fit(pairs):
        batch = dict(pairs)
        context = {bid: contexts[bid] for bid in batch if bid in contexts}
        words = " ".join(plain(term).casefold() for term in batch.values())
        words += " " + " ".join(c.get("parent", "").casefold() for c in context.values())
        relevant = {k: v for k, v in known.items() if k in words}
        prompt = lang.prompt("index")[0].replace("{to}", to)
        prompt += "\n\nGLOSSARY:\n" + json.dumps(relevant, ensure_ascii=False)
        prompt += "\n\nCONTEXT:\n" + json.dumps(context, ensure_ascii=False)
        prompt += "\n\nENTRIES:\n" + json.dumps(batch, ensure_ascii=False)
        prompt = P.boxed(prompt, "INDEX", "количество записей в JSON-объекте")
        size = len((f"{system}\n\n---\n\n{prompt}" if system else prompt).encode("utf-8"))
        if size <= AGY_CAP:
            yield batch, prompt
        elif len(pairs) > 1:
            middle = len(pairs) // 2
            yield from fit(pairs[:middle])
            yield from fit(pairs[middle:])
        else:
            raise Fatal(lang.T("index_oversize", size // 1000, AGY_CAP // 1000))

    pairs = list(todo.items())
    for start in range(0, len(pairs), INDEX_BATCH):
        yield from fit(pairs[start:start + INDEX_BATCH])


def translate(work, blocks, to, who, system, retries, log):
    from . import pipeline as P
    path = P.lpath(work, "index.json", to)
    saved = _load(path)
    known, contexts, fresh_hashes = _inputs(work, blocks, to)
    requests = _requests(blocks)
    result = saved.get("terms", {})
    hashes = saved.get("src", {})
    todo = {}
    for bid, term in requests.items():
        fp = fresh_hashes[bid]
        if hashes.get(bid) == fp and result.get(bid):
            continue
        canonical = known.get(plain(term).casefold())
        if canonical and not re.search(r"</?a\d+>", term):
            result[bid] = canonical
            hashes[bid] = fp
        else:
            todo[bid] = term
    done = 0
    usage = saved.get("usage", [])
    for batch, prompt in _batches(todo, contexts, known, to, system):
        def parse(out):
            data = json.loads(P.unbox(out, "INDEX"))
            if not isinstance(data, dict) or set(data) != set(batch):
                raise ValueError("index response must contain exactly the requested IDs")
            for bid, text in data.items():
                if not isinstance(text, str) or not plain(text):
                    raise ValueError(f"empty index term: {bid}")
                tags = re.findall(r"</?a\d+>", text)
                if tags != re.findall(r"</?a\d+>", batch[bid]):
                    raise ValueError(f"changed link markers in index term: {bid}")
                if re.search(r"<(?!/?(?:a\d+|i|b|sub|sup)>)[^>]+>", text):
                    raise ValueError(f"unknown markup in index term: {bid}")
            return data
        got, meta, _ = P._chain_run(who, system, prompt, retries, parse, log)
        result.update(got)
        hashes.update({bid: fresh_hashes[bid] for bid in batch})
        usage.append(P._spent(meta))
        done += len(batch)
        log(lang.T("index_done", done, len(todo)))
        P._save(path, {"terms": result, "src": hashes, "usage": usage})
    if requests:
        P._save(path, {"terms": result, "src": hashes, "usage": usage})
    return done


@lru_cache(maxsize=1)
def _collator():
    from pyuca import Collator
    return Collator()


def sort_key(term, to):
    text = unicodedata.normalize("NFC", plain(term)).casefold().lstrip("\"'«»“”‘’([{ ")
    if to == "ru":
        text = text.replace("ё", "е")
    # In a Russian index, untranslated Latin symbols follow Cyrillic entries.
    group = int(bool(text) and not ("а" <= text[0] <= "я")) if to == "ru" else 0
    return group, _collator().sort_key(text)


def _index_links(out, original):
    """Old letter divisions have no translated equivalent; link to the index."""
    available = {b["id"] for b in out if not b.get("drop")}
    removed = {b["id"]: b["index"]["section"] for b in original
               if b.get("index") and b["id"] not in available}
    result = []
    for b in out:
        links = []
        for url in b.get("links", []):
            if url.startswith("#") and url[1:] in removed:
                section = removed[url[1:]]
                url = "#" + section if section in available else ""
            links.append(url)
        if links != b.get("links", []):
            b = dict(b, links=links)
        result.append(b)
    return result


def assemble(work, blocks, meta, partial=False):
    """Sorted blocks and translations; no model calls during build."""
    from . import pipeline as P
    requests = _requests(blocks)
    if not requests:
        return _index_links(blocks, blocks), {}
    to = meta.get("target_lang", "")
    saved = _load(P.lpath(work, "index.json", to))
    terms = saved.get("terms", {})
    _, _, fresh_hashes = _inputs(work, blocks, to)
    stale = [bid for bid, term in requests.items()
             if not terms.get(bid) or saved.get("src", {}).get(bid) != fresh_hashes[bid]]
    if stale and not partial:
        raise SystemExit(lang.T("index_missing", len(stale)))
    terms = {bid: terms.get(bid, term) if bid not in stale else term for bid, term in requests.items()}
    mode = meta.get("index_mode", "auto")
    bilingual = mode == "bilingual" or mode == "auto" and meta.get("bilingual")
    by_id = {b["id"]: b for b in blocks}
    rendered, groups = {}, defaultdict(list)
    for b in blocks:
        info = b.get("index")
        if not info or b.get("drop") or info.get("divider"):
            continue
        bid = b["id"]
        term = terms[bid]
        tail = info.get("tail", "")
        for n, url in enumerate(b.get("links", []), 1):
            target = url[1:] if url.startswith("#") else ""
            if target in terms:
                tail = re.sub(rf"(<a{n}>).*?(</a{n}>)", lambda m: m[1] + plain(terms[target]) + m[2], tail, flags=re.S)
        tail = SEE.sub(lambda m: terms["_see_also"] if re.search(r"also|также|auch|aussi|también", m[0], re.I) else terms["_see"], tail)
        if bilingual and not info.get("heading") and plain(term).casefold() != plain(info["term"]).casefold():
            term += " (" + plain(info["term"]) + ")"
        depth, parent = 0, info.get("parent")
        while parent in by_id:
            depth += 1
            parent = by_id[parent].get("index", {}).get("parent")
        rendered[bid] = "  " * depth + ("— " if depth else "") + term + tail
        groups[info["section"]].append(b)
    ordered = {}
    for section, entries in groups.items():
        children = defaultdict(list)
        for b in entries:
            if not b["index"].get("heading"):
                children[b["index"].get("parent", "")].append(b)
        out = [b for b in entries if b["index"].get("heading")]
        def walk(parent):
            letter = None
            for b in sorted(children[parent], key=lambda b: sort_key(terms[b["id"]], to)):
                if not parent:
                    label = plain(terms[b["id"]]).lstrip("\"'«»“”‘’([{ ")[:1].upper()
                    if to == "ru":
                        label = label.replace("Ё", "Е")
                    if label != letter:
                        letter = label
                        lid = f"{section}_letter{len(out)}"
                        out.append({"id": lid, "kind": "subtitle", "text": label, "asis": True})
                        rendered[lid] = label
                out.append(b)
                walk(b["id"])
        walk("")
        ordered[section] = out
    out, seen = [], set()
    for b in blocks:
        info = b.get("index")
        if not info:
            out.append(b)
        elif info["section"] not in seen:
            out.extend(ordered.get(info["section"], []))
            seen.add(info["section"])
    return _index_links(out, blocks), rendered
