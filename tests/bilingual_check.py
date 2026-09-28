#!/usr/bin/env python3
"""Двуязычная сборка: под абзацем — оригинал, без знаков сносок, во всех
писателях; заголовки не дублируются. И отказ на зашифрованном EPUB.

    python3 tests/bilingual_check.py
"""
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from booktrans import extract as X, lang, output as O  # noqa: E402


def main():
    bad = seen = 0
    lang.set_ui("ru")

    def ok(name, cond, got=""):
        nonlocal bad, seen
        seen += 1
        print(f"  {name:52} {'совпадает' if cond else 'РАСХОЖДЕНИЕ'}" + ("" if cond else f"   вышло: {got}"))
        bad += not cond

    d = tempfile.mkdtemp()
    meta = {"title": "Книга", "author": "Автор", "target_lang": "ru"}
    items = [("title", "Глава", "s01.b0001", None, None, 1),
             ("p", "Перевод абзаца.", "s01.b0002", None, None, None),
             ("orig", "The <i>original</i> paragraph.", "s01.b0002_orig", None, None, None),
             ("verse", "строка", "s01.b0003", None, None, None),
             ("orig", "a line", "s01.b0003_orig", None, None, None)]
    notes = {"s01.b0002": {"text": "сноска"}}
    p = os.path.join(d, "b.epub")
    O.write_epub(p, meta, items, notes, {}, "Прим. ")
    z = zipfile.ZipFile(p)
    body = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    ok("epub: оригинал абзацем класса orig, с курсивом",
       '<p class="orig">The <i>original</i> paragraph.</p>' in body, body[:300])
    ok("epub: знак сноски у перевода, у оригинала нет",
       body.count("<sup>") == 1 and "orig\">The" in body)
    ok("epub: заголовок главы один, оригинала у него нет", body.count("<h1>Глава</h1>") == 1 and "orig\">Глава" not in body)
    h = os.path.join(d, "b.html"); O.write_html(h, meta, items, notes, {}, "Прим. ")
    ok("html: оригинал есть", 'class="orig"' in open(h, encoding="utf-8").read())
    m = os.path.join(d, "b.md"); O.write_md(m, meta, items, notes, {}, "Прим. ")
    ok("md: оригинал цитатой", "> The *original* paragraph." in open(m, encoding="utf-8").read(),
       [l for l in open(m, encoding="utf-8").read().splitlines() if "original" in l])
    t = os.path.join(d, "b.txt"); O.write_txt(t, meta, items, notes, {}, "Прим. ")
    ok("txt: оригинал отступом", "    | The original paragraph." in open(t, encoding="utf-8").read())

    # DRM: зашифрован текст — отказ; зашифрованы только шрифты — книга читается
    def epub(enc):
        p = os.path.join(d, "drm.epub")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata><manifest/><spine/></package>')
            if enc:
                z.writestr("META-INF/encryption.xml", f'<encryption><EncryptedData><CipherData><CipherReference URI="{enc}"/></CipherData></EncryptedData></encryption>')
        return p
    try:
        X._epub(epub("OEBPS/ch01.xhtml"))
        drm = None
    except SystemExit as e:
        drm = str(e)
    ok("зашифрованный текст — остановка с объяснением", drm is not None and "DRM" in drm, drm)
    try:
        X._epub(epub("OEBPS/fonts/a.ttf"))
        fonts = "прошло"
    except SystemExit as e:
        fonts = str(e)
    except Exception as e:                   # пустая книга ломается дальше — не о DRM
        fonts = "прошло" if "DRM" not in str(e) else str(e)
    ok("зашифрованы только шрифты — не DRM", fonts == "прошло", fonts)

    print(f"\n{'ВСЁ СОВПАДАЕТ' if not bad else f'РАСХОЖДЕНИЙ: {bad}'} ({seen} проверок)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
