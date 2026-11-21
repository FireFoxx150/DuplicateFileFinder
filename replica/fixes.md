# Fixes and positioning for DuplicateFileFinder

Sample: 41 public items from 3 sources. 27 GitHub issues (dupeGuru and czkawka), 9 Hacker News comments, 5 Reddit posts. None has a star rating. No App Store or Google Play reviews were collected. The Reddit czkawka search returned only release announcements, so czkawka's Reddit feedback is not represented. This is a small sample, mostly bug reports, and not a trend study. Read every theme below as thin.

## 1. What they hate

1. **Slow, stuck or heavy on memory.** 9 items, 2 sources. Example: "The scan has been running non-stop for four days." ([dupeGuru #1362](https://github.com/arsenetar/dupeguru/issues/1362)). Also "that program started eating 130gb of ram" ([dupeGuru #1344](https://github.com/arsenetar/dupeguru/issues/1344)) and "very slow due to Python's image processing libraries" ([HN](https://news.ycombinator.com/item?id=27635721)).
2. **Misses real duplicates.** 4 items, 2 sources. Example: "However my issue is that I am running into very SIMILAR files (not exact dupes) which DupeGuru is not flagging." ([Reddit](https://www.reddit.com/r/datacurator/comments/s109aw/help_looking_for_software_that_can_analyze/)). Also "dupeGuru ... does not find files with the same name that only differ in their extension" ([#1351](https://github.com/arsenetar/dupeguru/issues/1351)).
3. **Fear of losing files.** 3 items, 2 sources. "why would I trust a third party for my data and wrote a dupe finder" ([HN](https://news.ycombinator.com/item?id=23052946)). Thin: three items.
4. **Flags files that are not duplicates.** Thin, 2 items. "Large images ... are marked as dupes when they are completely different" ([czkawka #2093](https://github.com/qarmin/czkawka/issues/2093)).
5. **Excluded subfolders.** Thin, 2 items. "Excluded folder cannot be added when it is a subfolder of another folder" ([dupeGuru #1328](https://github.com/arsenetar/dupeguru/issues/1328)).

## 2. What is missing

1. **Choosing which copy to keep.** 4 items, 3 sources. "it doesn't choose the best one to keep: it will suggest to keep a version where people have their eyes closed" ([HN](https://news.ycombinator.com/item?id=40379388)). Also "keep the most original one (best resolution and most original metadata, especially the date)" ([Reddit](https://www.reddit.com/r/DataHoarder/comments/1nfkxyz/best_software_for_deduplicating_images/)).
2. **Similar, not identical files.** 4 items, 3 sources. Users want near-duplicates found, and the matching must not be fooled by unrelated images.
3. **Comparing two folders by identity or percentage.** Thin, 3 items. "looking for missing files or even knowing how similar in percentage they are" ([czkawka #2091](https://github.com/qarmin/czkawka/issues/2091)).
4. **Saved reference sets.** Thin, 1 item. "sets of paths, the reference path in this set and the comparison method could be saved" ([czkawka #2107](https://github.com/qarmin/czkawka/issues/2107)). This app already saves profiles, so this is a check, not a build.
5. **Partial results on cancel.** Thin, 1 item. "It would be nice if there was a choice to perform the selected function on the scanned objects when cancelling the task." ([czkawka #2046](https://github.com/qarmin/czkawka/issues/2046))

## 3. What is unsolved

1. **Huge libraries.** Users with millions of files or many terabytes are not served well by the tools they try. "Dupeguru was not up to this." ([Reddit](https://www.reddit.com/r/DataHoarder/comments/1ipvfnf/massive_deduping_job_millions_of_files_terabyes/)). Thin: 2 items.
2. **Photo keepers by quality, not by size.** Users want sharpness, open eyes and original date. Nothing in the reviews shows a tool that does this well. Thin: 3 items.

## Fix plan

Ranked by evidence and cost. Each fix must be checked against the code before building, because several gaps may already be closed.

| # | Fix | Size | Priority | Evidence |
| --- | --- | --- | --- | --- |
| 1 | Keeper rules for sharpness and original date, not just resolution | M | must | HN 40379388, Reddit 1nfkxyz, HN 27635769 |
| 2 | Regression test: large, dissimilar images must not group (colour check added in 2.7.1, width case untested) | S | must | czkawka #2093 |
| 3 | Check memory on a 100k-file folder, and stream hashing if RAM grows with file count | M | must | dupeGuru #1344, #1362 |
| 4 | Keep partial results when a scan is cancelled | S | should | czkawka #2046 |
| 5 | Name match mode that ignores extension case | S | should | dupeGuru #1351, czkawka #2138 (thin) |
| 6 | Two-folder summary: identical, only-in-A, only-in-B, and percentage similar | S | should | czkawka #2091 |
| 7 | Verify a subfolder can be excluded inside an included folder | S | should | dupeGuru #1328 (thin) |

Not planned, and why:
- **Permanent delete fears.** This app already sends to the trash, keeps a history and has a dry run. Use it in the angle, not as a build.
- **Saved profiles.** Already exist.
- **Installer and platform requests** (Fedora RPM, Flathub, Qt6, Homebrew). Packaging scope, not product scope. Revisit after the core fixes.
- **Screen reader support** ([czkawka #2115](https://github.com/qarmin/czkawka/issues/2115)). Real accessibility work, out of this pass. The app's README already states it does not support screen readers.

## Angle

Three options, each grounded in a theme:

1. For people with large photo libraries who have lost trust in duplicate finders that delete the wrong copy: **a finder that keeps the best copy and never deletes without a dry run, a trash step and an undo.** Evidence: keeper choice (3 sources), data safety (2 sources).
2. For people who run a duplicate finder on a slow or memory-hungry machine: **a finder that stays responsive on large folders.** Evidence: speed and memory (2 sources), thin.
3. For people comparing two backups: **a two-folder comparison that says what is missing and how similar the folders are.** Evidence: folder compare (thin, 3 items).

Recommended: **option 1.** It is backed by the most sources and matches the strongest existing features here (trash, history, dry run, keeper rules). It also gives the next skill, replica-brand, a voice to work with: careful, reversible, honest about what will be deleted.

Do not use the original names in the app name, ads or store listing. Do not put reviewer quotes on any landing page. A factual comparison is a legal question for a lawyer in your country.

## Next

`/replica-brand`. The sample is small and mostly GitHub bug reports, so before any public claim, collect Google Play and App Store reviews where they exist, and more Reddit complaints for czkawka.
