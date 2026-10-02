#!/usr/bin/env python3
"""Synthetic linked EPUB indexes: parsing, cache migration, translation and build.

No copyrighted text or model calls. Runs on Linux and Windows.
"""
import copy
import base64
import json
import os
import posixpath
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from booktrans import build as B, extract as E, indexing as I, pipeline as P  # noqa: E402

PAGE = '''<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Example</title></head><body>
<h1>Chapter</h1><p id="page12">Anaemia and drift are sample technical terms.</p>
<p id="page13">This second paragraph supplies context.</p>
<section epub:type="index"><h1>Index</h1><h2>Z</h2><ul>
 <li id="parent">Zulu<ul>
  <li id="child">drift, <a href="#page12">12</a></li>
  <li>alpha, <a href="#page13">13</a></li></ul></li>
 <li>Anaemia, <a href="#page12">12</a>–13</li>
 <li>Cross term. <i>See also</i> <a href="#parent">Zulu</a></li>
 <li>Unlinked, 40</li></ul></section>
<h1>Afterword</h1><p>Ordinary prose after the index.</p></body></html>'''

LABELS = {"Index": "Предметный указатель", "Zulu": "Якорь", "drift": "дрейф",
          "alpha": "альфа", "Anaemia": "Анемия", "Cross term": "Перекрёстный термин",
          "Unlinked": "Бессылочный", "see": "см.", "see also": "см. также"}


def answer_box(data):
    return "[[[INDEX 1]]]\n" + json.dumps(data, ensure_ascii=False) + "\n[[[/INDEX 1]]]"


def epub(path, page=PAGE):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>''')
        z.writestr("OPS/book.opf", '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Example</dc:title><dc:language>en</dc:language></metadata>
<manifest><item id="text" href="book.xhtml" media-type="application/xhtml+xml"/></manifest>
<spine><itemref idref="text"/></spine></package>''')
        z.writestr("OPS/book.xhtml", page)


def dump(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)


class IndexChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = self.temp.name
        self.source = os.path.join(self.work, "source.epub")
        epub(self.source)
        self.meta, self.blocks, _, _ = E.read_book(self.source)
        self.meta.update(target_lang="ru", index_mode="auto")
        self.entries = [b for b in self.blocks if b.get("index")]
        self.body = [b for b in self.blocks if not b.get("index")]
        self.translations = {b["id"]: "Перевод: " + b["text"] for b in self.body}
        dump(P.lpath(self.work, "tr/0001.json", "ru"), {"tr": self.translations})
        self.prompts = []

    def model(self, who, system, prompt, retries, parse, log):
        self.prompts.append(prompt)
        batch = json.JSONDecoder().raw_decode(prompt.split("ENTRIES:\n", 1)[1])[0]
        answer = {bid: LABELS[term] for bid, term in batch.items()}
        return parse(answer_box(answer)), {"model": "synthetic", "cost_usd": 0}, 0

    def translate(self):
        with patch.object(P, "_chain_run", self.model):
            return I.translate(self.work, self.blocks, "ru", [], "", 1, lambda *a, **k: None)

    def test_parent_and_cross_reference(self):
        parent = next(b for b in self.entries if b["text"] == "Zulu")
        child = next(b for b in self.entries if b["text"].startswith("drift,"))
        cross = next(b for b in self.entries if b["text"].startswith("Cross term"))
        self.assertEqual(child["index"]["parent"], parent["id"])
        self.assertEqual(cross["links"], ["#" + parent["id"]])
        self.assertEqual(len([b for b in self.entries if not b["index"].get("heading") and not b["index"].get("divider")]), 6)
        self.assertTrue(all(not b.get("drop") and b.get("asis") for b in self.entries))
        self.assertTrue(all(not b.get("index") for b in self.body))

    def test_index_does_not_change_prose_chunk_numbers(self):
        self.assertEqual(P.make_chunks(self.blocks), P.make_chunks(self.body))

    def test_old_letter_links_and_omitted_index(self):
        self.translate()
        divider = next(b for b in self.entries if b["index"].get("divider"))
        parent = next(b for b in self.entries if b["text"] == "Zulu")
        ref = {"id": "index-ref", "kind": "p", "text": "<a1>Index letter</a1>; <a2>Parent</a2>",
               "links": ["#" + divider["id"], "#" + parent["id"]]}
        blocks = [ref] + copy.deepcopy(self.blocks)
        out, _ = I.assemble(self.work, blocks, self.meta)
        self.assertEqual(out[0]["links"][0], "#" + divider["index"]["section"])
        I.mark(blocks, "omit")
        out, rendered = I.assemble(self.work, blocks, self.meta)
        self.assertEqual(out[0]["links"], ["", ""])
        self.assertEqual(ref["links"], ["#" + divider["id"], "#" + parent["id"]])
        self.assertFalse(rendered)
        from booktrans import output as O
        self.assertEqual(O._inline(ref["text"], O.HTML_INLINE, out[0]["links"]), "Index letter; Parent")

    def test_pagebreak_anchors_in_nested_entries(self):
        page = PAGE.replace('<li id="child">drift,', '<li id="child"><span id="indexpage" epub:type="pagebreak"/>drift,')
        page = page.replace('<h1>Chapter</h1>', '<h1>Chapter</h1><p><a href="#indexpage">Index page</a></p>')
        epub(self.source, page)
        _, blocks, _, _ = E.read_book(self.source)
        child = next(b for b in blocks if b["text"].startswith("drift,"))
        ref = next(b for b in blocks if "Index page" in b["text"])
        self.assertIn("indexpage", child["anchors"])
        self.assertEqual(ref["links"], ["#" + child["id"]])
        old = copy.deepcopy(blocks)
        for b in old:
            b.pop("index", None)
        restored = I.restore(self.source, self.work, old)
        new_child = next(b for b in restored if b["text"].startswith("drift,"))
        new_ref = next(b for b in restored if "Index page" in b["text"])
        self.assertEqual(new_ref["links"], ["#" + new_child["id"]])

    def test_threshold_and_explicit_modes(self):
        blocks = copy.deepcopy(self.blocks)
        for b in blocks:
            if b.get("index") and not b["text"].startswith("Anaemia"):
                b.pop("links", None)
        self.assertFalse(I.mark(blocks))
        self.assertTrue(I.mark(blocks, "translated"))
        self.assertTrue(I.mark(blocks, "bilingual"))
        self.assertFalse(I.mark(blocks, "omit"))
        for b in blocks:
            b.pop("links", None)
        self.assertFalse(I.mark(blocks, "translated"))
        # Exactly half of leaf records have working links; unlinked parents
        # are structural and do not dilute the denominator.
        sample = [{"id": "body", "kind": "p", "text": "Body"},
                  {"id": "root", "kind": "p", "text": "Group", "index": {"section": "sec", "parent": ""}},
                  {"id": "a", "kind": "p", "text": "A", "index": {"section": "sec", "parent": "root"}, "links": ["#body"]},
                  {"id": "b", "kind": "p", "text": "B, 2", "index": {"section": "sec", "parent": "root", "tail": ", 2"}}]
        self.assertEqual(I.mark(sample), {"sec"})

    def test_split_and_russian_navigation(self):
        self.assertEqual(I.split_entry("Term, <a1>12</a1>–14"), ("Term", ", <a1>12</a1>–14"))
        self.assertEqual(I.split_entry("Term, 12, 15"), ("Term", ", 12, 15"))
        self.assertEqual(I.split_entry("Term. см. <a1>Other</a1>")[0], "Term")
        self.assertEqual(I.SEE.sub("SEE", "см. Other; см. также Else"), "SEE Other; SEE Else")

    def test_payload_limit_includes_system_and_utf8_context(self):
        from booktrans.agent import Fatal
        todo = {str(n): "Термин " + str(n) for n in range(4)}
        contexts = {bid: {"parent": "Родитель", "passages": [{"translation": "я" * 700}]} for bid in todo}
        system = "Системные указания " * 25
        single = list(I._batches({"0": todo["0"]}, contexts, {}, "ru", system))[0][1]
        cap = len(f"{system}\n\n---\n\n{single}".encode("utf-8")) + 50
        with patch.object(I, "AGY_CAP", cap):
            batches = list(I._batches(todo, contexts, {}, "ru", system))
            self.assertEqual(len(batches), 4)
            self.assertEqual([bid for batch, _ in batches for bid in batch], list(todo))
            self.assertTrue(all(len(f"{system}\n\n---\n\n{prompt}".encode("utf-8")) <= cap for _, prompt in batches))
        with patch.object(I, "AGY_CAP", 1):
            with self.assertRaises(Fatal):
                list(I._batches(todo, contexts, {}, "ru", system))

    def test_semantic_boundaries_flat_entries_and_no_false_heading(self):
        page = PAGE.replace("<h1>Chapter</h1>", "<h1>Index investing</h1>")
        page = page.replace("<h1>Index</h1><h2>Z</h2>", "")
        page = page.replace("<h1>Afterword</h1>", "")
        epub(self.source, page)
        _, blocks, _, _ = E.read_book(self.source)
        self.assertFalse(next(b for b in blocks if b["text"] == "Index investing").get("index"))
        self.assertFalse(blocks[-1].get("index"))
        self.assertTrue(any(b.get("index", {}).get("heading") for b in blocks))
        root = ET.fromstring('<body><h1>Index</h1><p class="indexMain">Group</p><p class="indexSub">Child, 12</p><h1>Afterword</h1><p>Prose</p></body>')
        roles = I.doc_roles(root)
        ps = list(root.iter("p"))
        self.assertEqual(roles[id(ps[1])]["parent"], roles[id(ps[0])]["key"])
        self.assertNotIn(id(ps[2]), roles)

    def test_cache_resume_context_and_glossary(self):
        with patch.object(I, "glossary", return_value={"anaemia": "Анемия"}):
            self.assertGreater(self.translate(), 0)
            count = len(self.prompts)
            self.assertEqual(self.translate(), 0)
            self.assertEqual(len(self.prompts), count)
            self.assertIn('"translation":', self.prompts[0])
            requested = json.JSONDecoder().raw_decode(self.prompts[0].split("ENTRIES:\n", 1)[1])[0]
            self.assertNotIn("Anaemia", requested.values())
            # The edited destination text is part of the resume fingerprint.
            bid = next(b["id"] for b in self.body if b["kind"] == "p")
            self.translations[bid] = "Исправленная терминология."
            dump(P.lpath(self.work, "tr/0001.json", "ru"), {"tr": self.translations})
            with self.assertRaises(SystemExit):
                I.assemble(self.work, self.blocks, self.meta)
            self.assertGreater(self.translate(), 0)
            I.assemble(self.work, self.blocks, self.meta)
        with patch.object(I, "glossary", return_value={"anaemia": "Малокровие"}):
            self.translate()
            _, rendered = I.assemble(self.work, self.blocks, self.meta)
            self.assertTrue(any(s.startswith("Малокровие,") for s in rendered.values()))

    def test_sorted_hierarchy_and_display_modes(self):
        self.translate()
        ordered, rendered = I.assemble(self.work, self.blocks, self.meta)
        records = [b for b in ordered if b.get("index") and not b["index"].get("heading")]
        self.assertEqual([b["index"]["term"] for b in records], ["Anaemia", "Unlinked", "Cross term", "Zulu", "alpha", "drift"])
        self.assertEqual([b for b in ordered if not b.get("index") and b["id"] in self.translations], self.body)
        cross = next(b for b in records if b["index"]["term"] == "Cross term")
        self.assertIn("см. также</i> <a1>Якорь</a1>", rendered[cross["id"]])
        self.assertFalse(any("(Anaemia)" in s for s in rendered.values()))
        self.meta["bilingual"] = True
        _, rendered = I.assemble(self.work, self.blocks, self.meta)
        self.assertTrue(any("Анемия (Anaemia)" in s for s in rendered.values()))
        self.meta["index_mode"] = "translated"
        _, rendered = I.assemble(self.work, self.blocks, self.meta)
        self.assertFalse(any("(Anaemia)" in s for s in rendered.values()))
        self.meta.update(bilingual=False, index_mode="bilingual")
        _, rendered = I.assemble(self.work, self.blocks, self.meta)
        self.assertTrue(any("Анемия (Anaemia)" in s for s in rendered.values()))
        self.assertEqual(sorted(["Zulu", "Якорь", "Ёмкость", "Единица", "Анемия"], key=lambda s: I.sort_key(s, "ru")), ["Анемия", "Единица", "Ёмкость", "Якорь", "Zulu"])

    def test_restore_keeps_prose_and_anchors(self):
        old = copy.deepcopy([b for b in self.blocks if b["text"] != "Zulu"])
        for b in old:
            b.pop("index", None)
        before = copy.deepcopy(old)
        restored = I.restore(self.source, self.work, old)
        self.assertEqual(old, before)
        self.assertEqual([b for b in restored if not b.get("index")], self.body)
        self.assertEqual(len([b for b in restored if b.get("index")]), len(self.entries))
        parent = next(b for b in restored if b["text"] == "Zulu")
        self.assertIn(".x", parent["id"])
        cross = next(b for b in restored if b["text"].startswith("Cross term"))
        self.assertEqual(cross["links"], ["#" + parent["id"]])
        absent = I.restore(self.source, self.work, copy.deepcopy(self.body))
        self.assertEqual([b for b in absent if not b.get("index")], self.body)
        self.assertLess(next(i for i, b in enumerate(absent) if b.get("index")), next(i for i, b in enumerate(absent) if b["text"] == "Afterword"))

    def test_build_links_epub_html_fb2(self):
        self.translate()
        for ext in ("epub", "html", "fb2"):
            path = os.path.join(self.work, "out." + ext)
            B.build_book(self.work, self.meta, self.blocks, None, path, lambda *a, **k: None)
            if ext == "epub":
                with zipfile.ZipFile(path) as z:
                    documents = {n: ET.fromstring(z.read(n)) for n in z.namelist() if n.endswith(".xhtml")}
                    text = "".join(z.read(n).decode() for n in documents)
                    targets = {n: {el.get("id") for el in root.iter() if el.get("id")} for n, root in documents.items()}
                    for name, root in documents.items():
                        for el in root.iter():
                            url = el.get("href", "")
                            if "#" in url and not url.startswith(("http:", "https:")):
                                member, anchor = url.split("#", 1)
                                member = posixpath.normpath(posixpath.join(posixpath.dirname(name), member)) if member else name
                                self.assertIn(anchor, targets[member])
            else:
                with open(path, encoding="utf-8") as stream:
                    text = stream.read()
                anchors = set(re.findall(r'id="([^"]+)"', text))
                links = set(re.findall(r'href="#([^"]+)"', text))
                self.assertTrue(links <= anchors, (ext, links - anchors, anchors))
            self.assertIn("Предметный указатель", text)
            self.assertIn("Анемия", text)
            self.assertIn("Якорь", text)

    def test_response_validation(self):
        def invalid(who, system, prompt, retries, parse, log):
            batch = json.JSONDecoder().raw_decode(prompt.split("ENTRIES:\n", 1)[1])[0]
            with self.assertRaises(ValueError):
                parse(answer_box({}))
            good = {bid: LABELS[term] for bid, term in batch.items()}
            wrong = dict(good)
            wrong[next(iter(wrong))] = "<script>bad</script>"
            with self.assertRaises(ValueError):
                parse(answer_box(wrong))
            return parse(answer_box(good)), {"model": "synthetic", "cost_usd": 0}, 0
        with patch.object(P, "_chain_run", invalid):
            I.translate(self.work, self.blocks, "ru", [], "", 1, lambda *a, **k: None)

    def test_nonprose_link_destinations(self):
        pixel = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        targets = [{"id": "photo", "kind": "image", "text": "photo.png"},
                   {"id": "verse", "kind": "verse", "text": "Verse"},
                   {"id": "code", "kind": "code", "text": "print(1)"}]
        reference = {"id": "reference", "kind": "p", "text": "<a1>Image</a1>, <a2>Verse</a2>, <a3>Code</a3>",
                     "links": ["#photo", "#verse", "#code"]}
        blocks = [{"id": "chapter", "kind": "title", "text": "Chapter"}] + targets + [reference]
        dump(P.lpath(self.work, "tr/0002.json", "ru"), {"tr": {b["id"]: b["text"] for b in blocks}})
        for ext in ("epub", "html", "fb2"):
            path = os.path.join(self.work, "targets." + ext)
            B.build_book(self.work, self.meta, blocks, None, path, lambda *a, **k: None, images={"photo.png": pixel})
            if ext == "epub":
                with zipfile.ZipFile(path) as z:
                    text = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
            else:
                with open(path, encoding="utf-8") as stream:
                    text = stream.read()
            anchors = set(re.findall(r'id="([^"]+)"', text))
            self.assertTrue({"photo", "verse", "code"} <= anchors, (ext, anchors))


if __name__ == "__main__":
    unittest.main(warnings="ignore")
