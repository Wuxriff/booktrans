#!/usr/bin/env python3
"""Разметка Wiley: номер сноски и метка иллюстрации внутри <a role="doc-backlink">,
якорь на этой ссылке; подпись <caption> у таблицы; ссылки на таблицу и подпись.

    python3 tests/backlink_check.py
"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from booktrans import extract as X, lang, output as O  # noqa: E402

DOC = """<html xmlns="http://www.w3.org/1999/xhtml"><body>
<p>See <a href="#tab1">Table 2.3</a> and <a href="#fig1">Exhibit 1.1</a>.</p>
<table id="tab1"><caption>Table 2.3 Normal haemoglobins</caption><tr><td>Hb A</td><td>97%</td></tr></table>
<figure><figcaption><p><a href="#R_fig1" id="fig1" role="doc-backlink"><b>EXHIBIT 1.1</b></a> Caption.</p></figcaption></figure>
<p>Next paragraph.</p>
<ol>
<li class="noteEntry"><a href="#R_n1" id="n1" role="doc-backlink">1</a>. First note.</li>
<li class="noteEntry"><a href="#R_n2" id="n2" role="doc-backlink">2</a>. Second note.</li>
</ol>
<p>Old style note text. <a href="#back" role="doc-backlink">↩ back to text</a></p>
</body></html>"""


def main():
    bad = seen = 0
    lang.set_ui("ru")

    def ok(name, cond, got=""):
        nonlocal bad, seen
        seen += 1
        print(f"  {name:52} {'совпадает' if cond else 'РАСХОЖДЕНИЕ'}" + ("" if cond else f"   вышло: {got}"))
        bad += not cond

    blocks, anchors = X._doc_blocks(ET.fromstring(DOC), {}, lambda h: None, {})
    texts = [b[1] for b in blocks]
    ok("подпись таблицы — абзац перед таблицей",
       texts[1] == "Table 2.3 Normal haemoglobins" and blocks[2][0] == "table", texts[:3])
    ok("метка «EXHIBIT 1.1» в подписи сохранена", texts[3] == "<b>EXHIBIT 1.1</b> Caption.", texts[3])
    ok("номера сносок сохранены", texts[5] == "1. First note." and texts[6] == "2. Second note.", texts[5:7])
    ok("обратная ссылка в конце сноски снята", texts[7] == "Old style note text.", texts[7])
    ok("якорь таблицы ведёт к её подписи", anchors.get("tab1") == 1, anchors)
    ok("якорь иллюстрации — на подписи, а не на следующем абзаце", anchors.get("fig1") == 3, anchors)
    ok("якоря сносок — на своих сносках, без сдвига",
       anchors.get("n1") == 5 and anchors.get("n2") == 6, anchors)

    # id на таблице в готовой книге, когда на неё ссылаются
    d = tempfile.mkdtemp()
    items = [("p", "См. <a1>таблицу</a1>.", "s01.b0001", ["#s01.b0002"], None, None),
             ("table", "A | B", "s01.b0002", None, None, None)]
    p = os.path.join(d, "t.epub")
    O.write_epub(p, {"title": "К", "target_lang": "ru"}, items, {}, {}, "Прим. ")
    z = zipfile.ZipFile(p)
    body = "".join(z.read(n).decode() for n in z.namelist() if n.endswith("ch001.xhtml"))
    ok("epub: у таблицы-цели есть id", '<table id="s01.b0002"' in body, body[-300:])
    h = os.path.join(d, "t.html")
    O.write_html(h, {"title": "К", "target_lang": "ru"}, items, {}, {}, "Прим. ")
    ok("html: у таблицы-цели есть id", '<table id="s01.b0002"' in open(h, encoding="utf-8").read())

    print(f"\n{'ВСЁ СОВПАДАЕТ' if not bad else f'РАСХОЖДЕНИЙ: {bad}'} ({seen} проверок)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
