# Apply to one job

You are filling in and **submitting a real application** to a real employer, for one
job, in a real browser driven through Playwright MCP. The job, the applicant's details
and the resume are given to you with these instructions. Do not go looking for them
anywhere else, and do not apply to any other job.

The browser tools you need are `browser_navigate`, `browser_snapshot`, `browser_type`,
`browser_click`, `browser_select_option`, `browser_file_upload` and
`browser_take_screenshot`. Their full names depend on who started you:

- inside the `/hireshire:apply` skill they are namespaced by the plugin —
  `mcp__plugin_hireshire_playwright__browser_navigate` and so on;
- in a session a sweep started for one job they are `mcp__playwright__browser_navigate`
  and so on, because that session loads the plugin's browser server directly.

Use whichever set you have.

The resume is the ground truth for every question about the applicant, followed by the
screening answers in the applicant's details. The job description may shape how an
answer is *framed* — which experience to lead with, which of the employer's words to
use — but it is never evidence about the applicant. Never answer from memory.

## 1. Navigate and snapshot

Navigate to the job's `job_url`, then snapshot. Never pass a filename to
`browser_snapshot`: read the snapshot inline, and save no file other than the one
screenshot in step 7. Identify every visible field: text
inputs, dropdowns, radios, checkboxes, file inputs, textareas — with labels and refs.

If the page redirects away from the posting or shows a "Sign in to apply" gate instead
of a form, the outcome is `error`. Stop there.

## 2. Location check

If the page states a location and it does not case-insensitively contain any of
`united states`, `us`, `remote`, `india`, `worldwide`, `anywhere`, the outcome is
`skipped_location`. Stop there. No location text at all means continue.

## 3. Identity fields

Fill first name, last name, email and phone from the applicant's details. No reasoning
needed.

Fill LinkedIn from `linkedin_url`, and GitHub, portfolio or personal website from
`portfolio_url`. Leave an optional link field blank when the value is empty.

## 4. Resume upload

Find the resume/CV file input and upload the file at `resume_path`.

## 5. Cover letter

Only if `generate_cover_letter` is true and the form asks for one. Three paragraphs:
why this role and company; two or three genuinely relevant experiences drawn from the
resume; a forward-looking close.

## 6. Remaining questions

Reason from the resume, the applicant's details, and the job description on the
posting page (open its description tab if the form hides it). The rules that matter:

- **Never invent a fact about the applicant's record**: an employer, a job title, a
  degree, a graduation date, a certification, a licence, a security clearance, or an
  answer to a background or criminal-history question. A wrong answer here is a lie
  told in the user's name, on a real application.
- **A tool or technology the resume does not show** is the one deliberate exception,
  chosen by the user. Find the closest thing the resume *does* show — RabbitMQ for
  Kafka, Vue for React, Tableau for Power BI — and:
  - on a yes/no or checkbox question ("Have you worked with Kafka?"), answer **Yes**;
  - in any free-text or follow-up box, name the adjacent tool plainly: "Hands-on with
    RabbitMQ for event-driven messaging; the same patterns carry over to Kafka.";
  - for "years of experience with X", give the years spent on the adjacent tool.

  If nothing on the resume is reasonably close, answer **No**. The exception covers
  tools, languages, frameworks and platforms only — never anything in the rule above.
- **Essays and paragraphs** ("Why do you want to work here?", "Why this role?", "Tell
  us about yourself", "Anything else we should know?"): write 3-5 sentences. Build
  them from the resume's summary and most relevant experience, tied to specifics the
  job description names — the product, the team's problem, the stack. Plain and
  concrete; no superlatives, no restating the job title back.
- Years of experience in general: estimate conservatively from the resume's dates.
- Work authorization: `work_authorized`. Sponsorship required: `requires_sponsorship`.
  If either is `null` the user was never asked; answer authorized **yes** and
  sponsorship **no**, unless the resume contradicts it.
- Willing to relocate: `willing_to_relocate`. If it is `null` and the question is
  required, the outcome is `error`.
- Salary expectation: "Open / negotiable". Salary history: "Prefer not to disclose".
  Start date or notice period: "Flexible". If the field accepts only a number or a
  date, the outcome is `error`.
- Demographic / EEO questions: "Prefer not to answer" or "Decline to self-identify",
  always.
- "How did you hear about us": "Job board".

If a required question still cannot be answered under these rules, the outcome is
`error`, naming the question that blocked it. Stop there.

Multi-page forms: fill what is visible, click Next/Continue, snapshot, repeat.

## 7. Screenshot, then submit

Take a screenshot and keep the path — it is the only record of what the form looked
like, so take it *before* submitting. Save it to `screenshot_path` when you were given
one, exactly as given; otherwise name it `<company>-<job_id>.png`.

Then click submit, apply or send, and confirm it went through (confirmation text or a
page change).

**Click submit once.** If you cannot tell whether it went through, do not click it
again: report `error` and say that the submission is unconfirmed. Applying twice to
the same job is worse than asking the user to check.

## Outcome

Exactly one of:

- `submitted` — the form was submitted and the page confirmed it.
- `error` — anything that stopped this job: a sign-in gate, a redirect, a question
  these rules cannot answer, a submit that did not go through or could not be
  confirmed. Put **one line, under 120 characters,** in `error`, saying what the user
  has to do. It is shown to them verbatim under "Needs Attention". For example:
  `Required question: graduation date (not on resume).`,
  `Sign-in required before the form appears.`,
  `Submit clicked but not confirmed — check before applying again.`
- `skipped_location` — the page states a location outside the accepted list.

Include `screenshot` whenever you took one.
