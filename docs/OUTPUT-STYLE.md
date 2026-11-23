# lawkeep output style

How every lawkeep command talks to people. Code lives in `src/lawkeep/ui.py` (the Rich
building blocks) and `src/lawkeep/explain.py` (the shared sentences about laws). Follow this
guide in every command, hook message, error and `--help` text.

## Who we write for

Someone who has never heard of lawkeep, property-based testing or mutation testing, reading
in an 80-column terminal (or narrower). They want to know: **is everything fine, and if not,
what do I do?**

## The rules

1. **Lead with the answer.** The first thing on screen is the verdict (`ALL LAWS HOLD`,
   `SOME LAWS FAIL`, `WAITING FOR YOUR DECISION`, `SETUP PROBLEM`) or, for an item, the law's
   sentence. Details come after.
2. **Every number gets context.** Say what was counted and what it means.
   - Not "holds on today's code (46 examples tried)" but
     "Works with your current code — lawkeep tried 46 different inputs and the law was never broken."
   - Not "quick check: caught 3 of 3 dumb versions" but
     "Strong law — lawkeep made 3 deliberately broken versions of the code and this law caught all 3."
3. **Explain jargon once, or avoid it.** Use the glossary words below, the same way in every
   command. Never show internal words such as "vacuous", "mutant", "probe", "nodeid".
4. **Background last.** Law ids, file paths, history ids and timings are secondary: dim, after
   the important part, or behind `--details` (`check`, `strength`). The law *sentence* is
   always the primary name of a law.
5. **Always end with what to do next** (`Next step: ...`). If nothing is needed, say so.
6. **Weak laws are flagged loudly.** A yellow warning with the reason, and a recommendation
   ("Think twice: ...") on the approve screen. Never let a weak law look like a strong one.
7. **Short sentences.** One idea per sentence. Prefer "your code" over "the implementation".
8. **Honest wording.** Never "proven" or "guaranteed" below level L4. Say what was tried
   ("lawkeep tried 200 inputs"), not that something is impossible.
9. **Explain inputs.** When an example table shows `codes=[2, 1]`, say what `codes` is (from
   the law's `inputs={...}`; else plain words made from the generator or input name, such as
   "a sample charge that lawkeep made up for testing", plus a tip to add descriptions). Never
   show generator code such as `st.charges()`. Show input *values* in words too
   (`src/lawkeep/readable.py`): "charge: a charge of $0.25 with no discount code", "$" only
   for money-like names, "none"/"no <field>" for `None`, numbered parts for tuples of objects.
   When lawkeep is unsure, it shows the raw value; `--details` and `--json` always keep it.
10. **Say failures in words.** Not `assert 0.26 == 0.25` but "The law expected 0.25, but the
    code gave 0.26." (also "at most", "at least", "less than", "more than", "something other
    than"); show the raw check only when it cannot be translated. Label the failing input
    "The input that breaks it".
11. **Summarise long lists.** Many notes of the same kind become one line with a count and the
    command that shows them all (`run lawkeep check --details to see them`). Errors are never
    summarised away.

## Glossary (use these words, mean these things)

| Word | Plain meaning to show the first time it matters |
| --- | --- |
| law | a rule your code must always follow, written as one plain sentence plus a check lawkeep runs |
| draft | a law a coding agent wrote that nobody has approved yet; lawkeep does not check it until you approve it |
| proposal | a requested change to an approved law; weakening ones wait for your yes |
| input | a value lawkeep feeds to the law (it tries many different ones) |
| works with your current code | lawkeep tried many inputs and the law was never broken |
| breaks / fails | lawkeep found an input where the code does not follow the law |
| deliberately broken versions | copies of your code with a small mistake planted on purpose (for example "always returns 0"); a strong law notices them |
| strong / weak law | a strong law catches the deliberately broken versions; a weak law lets some through |
| one-sided | the law checks a limit ("never more than"), not the right answer |
| unrecorded change | a law file changed without going through lawkeep |
| stricter / looser | a change that makes the law demand more / less |
| profile | a set of check settings: `pr` (repeatable, the default) or `deep` (many more inputs) |
| saved failing example | an input that once broke a law; lawkeep tries it again on every check |
| waiting list | everything that waits for a person's decision, in one place (`lawkeep approve`); counted as "N things wait for your decision — run `lawkeep approve`" |
| approval made without a terminal | a yes a coding agent recorded on someone's behalf (from its permission prompt); a person confirms or undoes it |
| park | (for agents) propose the change a law blocks and keep going with other work; the change waits for the person |

## Layout

- **Verdict banner** (`ui.banner`): one box, the verdict word in capitals plus one sentence.
- **Item panel** (`ui.panel`): one box per draft, proposal or change. Inside: the sentence
  first, then plain checks (`ui.status` lines: ✓ works, ✓ strong / ! weak), then the examples
  table (Input / What the code did / Follows the law?), then what the inputs mean, then the
  recommendation. The law id and file go in the box's dim subtitle.
- **Tables** (`ui.table`): wrap between words, never wider than the terminal. Example tables
  have one row per example (all inputs on one line, e.g. "price $16.47, code SAVE25") and a
  short Note column only when a note says something ("zero", "written in the law").
- **Ids and paths** (`ui.ident`): dim, and a long id wraps after a dot or slash, never in the
  middle of a word.
- **Warnings** (`ui.warning`): yellow, start with what is wrong in plain words.
- **Next step** (`ui.next_step`): the last line.

## Modes

- Terminal: colour and rounded boxes, sized to the window.
- Plain (not a terminal, `CI` set, `NO_COLOR`, `TERM=dumb`): no ANSI codes, ASCII boxes,
  marks `+` (good), `!` (warning), `x` (problem), `-` (note); width from `COLUMNS` or 80.
  `LAWKEEP_COLOR=always` forces colour.
- `--json`: unchanged machine-readable reports; never formatted, never wrapped.
- `report --format markdown` (the pull-request comment): the same wording, Markdown, no Rich.
- Hook messages read by agents (guard denials, session start) are plain sentences with the
  exact command to run.

## Status words

| Situation | Word |
| --- | --- |
| a law held on every input tried | Holds |
| a law broke on an input | Fails |
| a law could not run | Could not run |
| skipped with a marker | Skipped |
| function in the protection report | Protected / Partly protected / Not protected — read this one |
| law change list | Added / Changed / Removed / Waiting for you / Rejected / Accepted by hand / Approval confirmed / Approval undone |
