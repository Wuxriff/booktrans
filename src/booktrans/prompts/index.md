Translate the supplied EPUB index terms into {to}.
Return a JSON object with exactly the supplied IDs and string values.
Use the book's glossary consistently. Terms must be dictionary headwords;
subentries must make sense under the supplied parent term.
Use the supplied source/translated passages to match terminology in the final
book. A link may point to a page start: the term need not appear literally in
the supplied passage. Do not invent a different link destination.
Preserve meaningful qualifiers, abbreviations, names and book titles. Preserve inline markup and
numbered <aN> link markers if present. Do not add page numbers, references,
explanations or the source term in parentheses. Do not sort the entries.
The reserved entries _see and _see_also are the index navigation labels.
