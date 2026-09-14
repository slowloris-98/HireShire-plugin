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

The resume is the ground truth for every question you answer. Do not answer from the
job description or from memory.

## 1. Navigate and snapshot

Navigate to the job's `job_url`, then snapshot. Identify every visible field: text
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

## 4. Resume upload

Find the resume/CV file input and upload the file at `resume_path`.

## 5. Cover letter

Only if `generate_cover_letter` is true and the form asks for one. Three paragraphs:
why this role and company; two or three genuinely relevant experiences drawn from the
resume; a forward-looking close.

## 6. Remaining questions

Reason from the resume, the job title and company, and the URL. The rules that matter:

- **Never fabricate experience or qualifications that are not in the resume.** This is
  the one hard rule — a wrong answer here is a lie told in the user's name, on a real
  application.
- Years of experience: estimate conservatively from the resume's dates.
- Demographic / EEO questions: "Prefer not to answer" or "Decline to self-identify",
  always.
- "How did you hear about us": "Job board".
- Work authorization: yes. Sponsorship required: no. Unless the resume contradicts it.

If a required question cannot be answered honestly from the resume, the outcome is
`error`, naming the question that blocked it. Stop there.

Multi-page forms: fill what is visible, click Next/Continue, snapshot, repeat.

## 7. Screenshot, then submit

Take a screenshot and keep the path — it is the only record of what the form looked
like, so take it *before* submitting.

Then click submit, apply or send, and confirm it went through (confirmation text or a
page change).

**Click submit once.** If you cannot tell whether it went through, do not click it
again: report `error` and say that the submission is unconfirmed. Applying twice to
the same job is worse than asking the user to check.

## Outcome

Exactly one of:

- `submitted` — the form was submitted and the page confirmed it.
- `error` — anything that stopped this job, with a one-sentence explanation in
  `error`: a sign-in gate, a redirect, a question that cannot be answered honestly, a
  submit that did not go through or could not be confirmed.
- `skipped_location` — the page states a location outside the accepted list.

Include `screenshot` whenever you took one.
