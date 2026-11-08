The gala's files, sorted by session. Don't put files here yourself: drop them on the portal's Gala tab, or
save them into the inbox folder (data/inbox unless the portal was started with --inbox), and the portal
sorts them within a few seconds:

  inbox/                     new files arrive here (the scanner, the Gen7 computer, the browser)
  sessions/S1/meet/          Meet Manager: the session's entries export, printed lane sheets (the PDF, not
                             the scan), merge results export, final export
  sessions/S1/gen7/          Gen7 race PDFs, one per race
  sessions/S1/scans/         lane sheet packs (one per event), DQ slips, ETD programme
  sessions/S1/ocr/           written by Claude (gala-ocr)
  sessions/S1/reports/       written by Claude: event and session audits, merge results .hy3, final audit
  unsorted/                  files the portal couldn't place (choose where they go on the Gala tab)
  state.json                 the portal's own state: ticks, what Claude was asked, the activity log

Name files so the portal can place them: put "Session 2" (or "S2") in a name for session 2, "Event 3" in a
lane sheet pack's name, "DQ" in DQ slip scans and "ETD" in the programme scan. Gen7 PDFs and Meet Manager exports
are recognised by their contents. A file with the same name as one already there replaces it (the old copy is
kept in a .replaced folder).

These files contain children's personal details. Keep them on this computer only.
For the next gala, move this folder's contents into an archive folder (or start a copy of the portal).
